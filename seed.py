# seed.py
import csv
import sys
import argparse
from datetime import date
from typing import Optional

from db import SessionLocal
from models import (
    Edition, Society, Person, EditionMember, EditionSociety,
    Round, Debate, DebatePosition,
)

# ---------------------------------
# Helpers genéricos (idempotentes)
# ---------------------------------

def _strip_or_none(s: Optional[str]) -> Optional[str]:
    return s.strip() if isinstance(s, str) else None

def get_or_create_society(sess, name: Optional[str], short_name: Optional[str], city: Optional[str] = None) -> Society:
    """
    Resolve society por short_name (preferência) ou name. Cria se não existir.
    """
    name = _strip_or_none(name)
    short_name = _strip_or_none(short_name)

    q = None
    if short_name:
        q = sess.query(Society).filter(Society.short_name == short_name).one_or_none()
        if q:
            # Atualiza name/city se vierem preenchidos
            if name and q.name != name:
                q.name = name
            if city and q.city != city:
                q.city = city
            sess.flush()
            return q

    if name:
        q = sess.query(Society).filter(Society.name == name).one_or_none()
        if q:
            if short_name and q.short_name != short_name:
                q.short_name = short_name
            if city and q.city != city:
                q.city = city
            sess.flush()
            return q

    # criar
    soc = Society(name=name or short_name, short_name=short_name, city=city)
    sess.add(soc)
    sess.flush()
    return soc

def get_or_create_person(sess, full_name: str, society: Optional[Society], email: Optional[str] = None) -> Person:
    """
    Identifica pessoa por (full_name, society_id) como chave prática.
    Se society for None, tenta por (full_name, society_id IS NULL).
    """
    full_name = full_name.strip()
    society_id = society.id if society else None

    q = (
        sess.query(Person)
        .filter(Person.full_name == full_name, Person.society_id == society_id)
        .one_or_none()
    )
    if q:
        if email and (q.email or "").strip() != (email or "").strip():
            q.email = email
        sess.flush()
        return q

    p = Person(full_name=full_name, email=email, society_id=society_id)
    sess.add(p)
    sess.flush()
    return p

def ensure_member(sess, edition: Edition, person: Person, kind: str) -> EditionMember:
    m = (
        sess.query(EditionMember)
        .filter_by(edition_id=edition.id, person_id=person.id, kind=kind)
        .one_or_none()
    )
    if m:
        return m
    m = EditionMember(edition_id=edition.id, person_id=person.id, kind=kind)
    sess.add(m)
    sess.flush()
    return m

def ensure_edition(sess, year: int, name: Optional[str] = None) -> Edition:
    e = sess.query(Edition).filter_by(year=year).one_or_none()
    if e:
        return e
    e = Edition(year=year, name=name or f"Taça das Sociedades {year}")
    sess.add(e)
    sess.flush()
    return e

def ensure_edition_society(sess, edition: Edition, society: Society) -> EditionSociety:
    es = (
        sess.query(EditionSociety)
        .filter_by(edition_id=edition.id, society_id=society.id)
        .one_or_none()
    )
    if es:
        return es
    es = EditionSociety(edition_id=edition.id, society_id=society.id)
    sess.add(es)
    sess.flush()
    return es

def ensure_round(sess, edition: Edition, silent: bool, number: int, name: Optional[str] = None, scheduled_date: Optional[date] = None) -> Round:
    r = (
        sess.query(Round)
        .filter_by(edition_id=edition.id, number=number)
        .one_or_none()
    )
    if r:
        # atualiza nome/data se vierem
        if name and r.name != name:
            r.name = name
        if scheduled_date and r.scheduled_date != scheduled_date:
            r.scheduled_date = scheduled_date
        sess.flush()
        return r
    r = Round(edition_id=edition.id, number=number, name=name, scheduled_date=scheduled_date, silent=silent)
    sess.add(r)
    sess.flush()
    return r

def ensure_debate(sess, round_obj: Round, number_in_round: int) -> Debate:
    d = (
        sess.query(Debate)
        .filter_by(round_id=round_obj.id, number_in_round=number_in_round)
        .one_or_none()
    )
    if d:
        return d
    d = Debate(round_id=round_obj.id, number_in_round=number_in_round)
    sess.add(d)
    sess.flush()
    return d

def upsert_debate_position(sess, debate: Debate, position: str, edition_society: EditionSociety) -> DebatePosition:
    dp = (
        sess.query(DebatePosition)
        .filter_by(debate_id=debate.id, position=position)
        .one_or_none()
    )
    if dp:
        if dp.edition_society_id != edition_society.id:
            dp.edition_society_id = edition_society.id
            sess.flush()
        return dp
    dp = DebatePosition(debate_id=debate.id, position=position, edition_society_id=edition_society.id)
    sess.add(dp)
    sess.flush()
    return dp

def _resolve_society(sess, ref: str) -> Society:
    """
    Resolve uma society a partir de um identificador (short_name ou name).
    Cria se não existir, usando o mesmo texto como 'name' ou 'short_name'.
    """
    ref = ref.strip()
    # tenta por short_name
    s = sess.query(Society).filter(Society.short_name == ref).one_or_none()
    if s:
        return s
    # tenta por name
    s = sess.query(Society).filter(Society.name == ref).one_or_none()
    if s:
        return s
    # criar novo (assume ref como short_name e name igual)
    return get_or_create_society(sess, name=ref, short_name=ref)

# ---------------------------------
# 1) Import de membros (debaters/judges)
# ---------------------------------

def import_members_csv(path: str, default_edition_year: Optional[int] = None) -> None:
    """
    Espera colunas:
      - edition_year (opcional se default_edition_year fornecido)
      - society_name (opcional)
      - society_short (opcional)
      - full_name (obrigatória)
      - email (opcional)
      - kind (obrigatória: 'debater' ou 'judge')
    """
    sess = SessionLocal()
    created = updated = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):  # 2 = 1-based + header
                year_str = (row.get("edition_year") or "").strip()
                year = int(year_str) if year_str else default_edition_year
                if not year:
                    raise ValueError(f"[{path}:{i}] edition_year ausente (sem default).")

                kind = (row.get("kind") or "").strip().lower()
                if kind not in {"debater", "judge", "both"}:
                    raise ValueError(f"[{path}:{i}] kind inválido: {kind!r}")

                full_name = (row.get("full_name") or "").strip()
                if not full_name:
                    raise ValueError(f"[{path}:{i}] full_name é obrigatório.")

                society_name = (row.get("society_name") or "").strip() or None
                society_short = (row.get("society_short") or "").strip() or None
                email = (row.get("email") or "").strip() or None

                edition = ensure_edition(sess, year)
                soc = get_or_create_society(sess, name=society_name, short_name=society_short)
                es = ensure_edition_society(sess, edition, soc)
                person = get_or_create_person(sess, full_name=full_name, society=soc, email=email)
                kind_list = ["debater", "judge"] if kind == "both" else [kind]
                for kind in kind_list:
                    _ = ensure_member(sess, edition, person, kind=kind)
                created += 1

        sess.commit()
        print(f"[OK] Import members: {created} linhas aplicadas.")
    except Exception as e:
        sess.rollback()
        print(f"[ERRO] Import members falhou: {e}")
        raise
    finally:
        sess.close()

# ---------------------------------
# 2) Import de pareamentos (OG/OO/CG/CO)
# ---------------------------------

def import_pairings_csv(path: str, default_edition_year: Optional[int] = None) -> None:
    """
    Espera colunas:
      - edition_year (opcional se default_edition_year fornecido)
      - round_number (obrigatória, int)
      - debate_number (obrigatória, int)
      - OG, OO, CG, CO (obrigatórias): podem ser short_name OU name da sociedade.
    Cria/atualiza Round, Debate e DebatePosition (idempotente).
    """
    sess = SessionLocal()
    applied = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):
                year_str = (row.get("edition_year") or "").strip()
                year = int(year_str) if year_str else default_edition_year
                if not year:
                    raise ValueError(f"[{path}:{i}] edition_year ausente (sem default).")

                try:
                    round_number = int((row.get("round_number") or "").strip())
                    debate_number = int((row.get("debate_number") or "").strip())
                    silent_status = (row.get("silent") == "TRUE")
                except ValueError:
                    raise ValueError(f"[{path}:{i}] round_number/debate_number inválidos.")

                og_ref = (row.get("OG") or "").strip()
                oo_ref = (row.get("OO") or "").strip()
                cg_ref = (row.get("CG") or "").strip()
                co_ref = (row.get("CO") or "").strip()
                if not all([og_ref, oo_ref, cg_ref, co_ref]):
                    raise ValueError(f"[{path}:{i}] OG/OO/CG/CO obrigatórios.")

                edition = ensure_edition(sess, year)
                rnd = ensure_round(sess, edition, silent_status, number=round_number)
                debate = ensure_debate(sess, rnd, number_in_round=debate_number)

                # Resolve societies
                og_soc = _resolve_society(sess, og_ref)
                oo_soc = _resolve_society(sess, oo_ref)
                cg_soc = _resolve_society(sess, cg_ref)
                co_soc = _resolve_society(sess, co_ref)

                # Garante inscrição das sociedades na edição
                og_es = ensure_edition_society(sess, edition, og_soc)
                oo_es = ensure_edition_society(sess, edition, oo_soc)
                cg_es = ensure_edition_society(sess, edition, cg_soc)
                co_es = ensure_edition_society(sess, edition, co_soc)

                # Upsert posições
                upsert_debate_position(sess, debate, "OG", og_es)
                upsert_debate_position(sess, debate, "OO", oo_es)
                upsert_debate_position(sess, debate, "CG", cg_es)
                upsert_debate_position(sess, debate, "CO", co_es)

                applied += 1

        sess.commit()
        print(f"[OK] Import pairings: {applied} debates aplicados.")
    except Exception as e:
        sess.rollback()
        print(f"[ERRO] Import pairings falhou: {e}")
        raise
    finally:
        sess.close()


def import_societies_provisorio(socs):
    sess = SessionLocal()

    for name, short_name, city in socs:
        get_or_create_society(sess, name, short_name=short_name, city=city)
    sess.commit()


if __name__ == "__main__":
    if 1:
        # socs = [
        #     ["Independente", "Independente", None],
        # ]
        # import_societies_provisorio(socs)
        import_members_csv("members2.csv", default_edition_year=2025)
        # import_pairings_csv("pairings.csv", default_edition_year=2025)
