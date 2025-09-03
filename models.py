from sqlalchemy import (
    Integer, String, Date, DateTime, Enum, ForeignKey, UniqueConstraint,
    func, CheckConstraint, Boolean, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db import Base

# ----------------------------
# Entidades de base e edição
# ----------------------------

class Society(Base):
    __tablename__ = "societies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    short_name: Mapped[str] = mapped_column(String(40), unique=True, nullable=True)
    city: Mapped[str] = mapped_column(String(80), nullable=True)   # country removido

    members: Mapped[list["Person"]] = relationship(back_populates="society", cascade="all,save-update")


class Edition(Base):
    __tablename__ = "editions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # start_date / end_date removidos a pedido

    societies: Mapped[list["EditionSociety"]] = relationship(back_populates="edition", cascade="all,delete-orphan")


class EditionSociety(Base):
    """
    Inscrição da sociedade em uma edição específica.
    """
    __tablename__ = "edition_societies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id", ondelete="CASCADE"), nullable=False, index=True)
    society_id: Mapped[int] = mapped_column(ForeignKey("societies.id", ondelete="RESTRICT"), nullable=False, index=True)

    edition: Mapped["Edition"] = relationship(back_populates="societies")
    society: Mapped["Society"] = relationship()

    __table_args__ = (
        UniqueConstraint("edition_id", "society_id", name="uq_edition_society"),
    )

# ----------------------------
# Pessoas e inscrição por edição
# ----------------------------

class Person(Base):
    """
    Pessoa vinculada a uma Society 'base'.
    """
    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(150), nullable=True, unique=False)
    society_id: Mapped[int | None] = mapped_column(ForeignKey("societies.id", ondelete="SET NULL"), nullable=True)

    society: Mapped["Society"] = relationship(back_populates="members")
    edition_memberships: Mapped[list["EditionMember"]] = relationship(back_populates="person", cascade="all,delete-orphan")


RoleEnum = Enum("normal", "director", "admin", name="role_enum")
MemberKindEnum = Enum("debater", "judge", name="member_kind_enum")

class EditionMember(Base):
    """
    Inscrição da pessoa em uma edição com um papel específico (debater ou judge).
    """
    __tablename__ = "edition_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(MemberKindEnum, nullable=False)  # 'debater' | 'judge'

    person: Mapped["Person"] = relationship(back_populates="edition_memberships")
    edition: Mapped["Edition"] = relationship()

    __table_args__ = (
        UniqueConstraint("edition_id", "person_id", "kind", name="uq_member_per_kind"),
    )

# ----------------------------
# Autenticação simples (opcional)
# ----------------------------

class User(Base):
    """
    Usuários para autenticação no app.
    'role' controla acesso: normal / director / admin.
    """
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(RoleEnum, nullable=False, default="normal")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # society_id removido

# ----------------------------
# Rodadas, debates e posições
# ----------------------------

PositionEnum = Enum("OG", "OO", "CG", "CO", name="position_enum")

class Round(Base):
    __tablename__ = "rounds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("editions.id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3, ...
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    scheduled_date: Mapped[Date | None] = mapped_column(Date, nullable=True)  # só o dia

    motion: Mapped[str | None] = mapped_column(Text, nullable=True)        # moção da rodada
    infoslide: Mapped[str | None] = mapped_column(Text, nullable=True)     # infoslide da rodada
    silent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # 'silent' indica se os resultados ficarão públicos (False) ou não (True)

    edition: Mapped["Edition"] = relationship()
    debates: Mapped[list["Debate"]] = relationship(back_populates="round", cascade="all,delete-orphan")

    __table_args__ = (
        UniqueConstraint("edition_id", "number", name="uq_round_per_edition"),
    )


class Debate(Base):
    """
    Um debate dentro de uma rodada.
    """
    __tablename__ = "debates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    number_in_round: Mapped[int] = mapped_column(Integer, nullable=False)  # ex.: 1 ou 2

    round: Mapped["Round"] = relationship(back_populates="debates")
    positions: Mapped[list["DebatePosition"]] = relationship(back_populates="debate", cascade="all,delete-orphan")
    judges: Mapped[list["DebateJudge"]] = relationship(back_populates="debate", cascade="all,delete-orphan")
    speeches: Mapped[list["Speech"]] = relationship(back_populates="debate", cascade="all,delete-orphan")

    __table_args__ = (
        UniqueConstraint("round_id", "number_in_round", name="uq_debate_per_round"),
    )

class DebatePosition(Base):
    """
    Mapeia qual sociedade (daquela edição) ocupa OG/OO/CG/CO.
    """
    __tablename__ = "debate_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    debate_id: Mapped[int] = mapped_column(ForeignKey("debates.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[str] = mapped_column(PositionEnum, nullable=False)
    edition_society_id: Mapped[int] = mapped_column(ForeignKey("edition_societies.id", ondelete="RESTRICT"), nullable=False, index=True)

    debate: Mapped["Debate"] = relationship(back_populates="positions")
    team: Mapped["EditionSociety"] = relationship()

    __table_args__ = (
        UniqueConstraint("debate_id", "position", name="uq_position_per_debate"),
    )

# ----------------------------
# Speeches (quem falou e nota)
# ----------------------------

class Speech(Base):
    """
    Armazena os 2 discursos por posição (OG/OO/CG/CO), com orador e nota 50–100.
    - sequence_in_team: 1 ou 2 (primeiro e segundo discurso daquele lado)
    - score inteiro (nullable até a publicação das notas)
    """
    __tablename__ = "speeches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    debate_id: Mapped[int] = mapped_column(ForeignKey("debates.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[str] = mapped_column(PositionEnum, nullable=False)  # OG/OO/CG/CO
    sequence_in_team: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 ou 2
    edition_member_id: Mapped[int] = mapped_column(ForeignKey("edition_members.id", ondelete="RESTRICT"), nullable=False, index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # inteiro
    # Restrições:
    __table_args__ = (
        UniqueConstraint("debate_id", "position", "sequence_in_team", name="uq_speech_slot"),
        CheckConstraint("sequence_in_team IN (1, 2)", name="ck_sequence_1_or_2"),
        CheckConstraint("(score IS NULL) OR (score BETWEEN 50 AND 100)", name="ck_score_50_100"),
    )

    debate: Mapped["Debate"] = relationship(back_populates="speeches")
    speaker: Mapped["EditionMember"] = relationship()

# ----------------------------
# Juízes do debate
# ----------------------------

JudgeRoleEnum = Enum("chair", "wing", name="judge_role_enum")

class DebateJudge(Base):
    __tablename__ = "debate_judges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    debate_id: Mapped[int] = mapped_column(ForeignKey("debates.id", ondelete="CASCADE"), nullable=False, index=True)
    edition_member_id: Mapped[int] = mapped_column(ForeignKey("edition_members.id", ondelete="RESTRICT"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(JudgeRoleEnum, nullable=False)  # chair | wing

    debate: Mapped["Debate"] = relationship(back_populates="judges")
    judge: Mapped["EditionMember"] = relationship()

    __table_args__ = (
        UniqueConstraint("debate_id", "edition_member_id", name="uq_judge_once_per_debate"),
    )
