"""ISO 22301 BCMS AI 모의심사 — Streamlit 엔트리포인트.

화면 흐름: team_select → scenario_select → quiz → result
상태 변경은 모두 버튼 on_click 콜백에서 처리해 rerun·중복 클릭 시 중복 반영을 막는다.
위젯 key의 값은 위젯이 화면에서 사라지면 삭제되므로, 이후 단계에서 쓰는 값은 일반 state key로 복사한다.
"""

import copy
from datetime import datetime, timedelta, timezone

import streamlit as st

from src import llm, quiz, ui
from src.config import load_settings
from src.models import QUESTION_COUNT, AnswerRecord
from src.report import ReportData, ReportError, build_report, report_filename
from src.teams import get_team, search_teams

KST = timezone(timedelta(hours=9))
ROLES = ["팀장", "정", "부", "구성원"]
SITES = ["전체", "당진", "순천"]

STEPS = {
    "team_select": "① 팀 선택",
    "scenario_select": "② 시나리오 선택",
    "quiz": "③ 심사 질의",
    "result": "④ 결과",
}

# 시나리오 이하 단계의 진행 상태 (재도전 시 이것만 초기화)
QUIZ_STATE = {
    "scenario": None,
    "questions": None,
    "q_index": 0,
    "answers": [],
    "completed_at": None,
    "recommendations": None,
    "pdf_bytes": None,
    "pdf_name": None,
    "pdf_error": None,
}

DEFAULT_STATE = {
    "step": "team_select",
    "picked_team": None,
    "team_name": None,
    "participant_name": "",
    "participant_role": "구성원",
    "error": None,
    **QUIZ_STATE,
}


def _apply(defaults: dict, only_missing: bool = False) -> None:
    for key, value in defaults.items():
        if not only_missing or key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def init_state() -> None:
    _apply(DEFAULT_STATE, only_missing=True)
    # 팀별 시나리오 캐시는 [처음으로]에도 유지 (같은 팀 재선택 시 재호출 방지)
    st.session_state.setdefault("scenario_cache", {})


def reset() -> None:
    _apply(DEFAULT_STATE)


def current_team():
    return get_team(st.session_state.team_name) if st.session_state.team_name else None


def now_kst() -> datetime:
    return datetime.now(KST)


# ── 콜백 ──────────────────────────────────────────────


def on_pick_team(name: str) -> None:
    st.session_state.picked_team = name


def on_select_team() -> None:
    name = st.session_state.picked_team
    if name is None:
        return
    participant_name = st.session_state.get("participant_name_input", "").strip()
    participant_role = st.session_state.get("participant_role_input", "구성원")
    reset()
    st.session_state.team_name = name
    st.session_state.participant_name = participant_name
    st.session_state.participant_role = participant_role
    st.session_state.step = "scenario_select"


def on_regenerate_scenarios() -> None:
    st.session_state.scenario_cache.pop(st.session_state.team_name, None)
    st.session_state.error = None


def on_start_quiz(index: int) -> None:
    scenarios = st.session_state.scenario_cache.get(st.session_state.team_name) or []
    # 재생성 중(캐시 비어 있음) 이전 화면에 남은 버튼을 누른 경우 무시
    if not 0 <= index < len(scenarios):
        return
    _apply(QUIZ_STATE)
    st.session_state.scenario = scenarios[index]
    st.session_state.error = None
    st.session_state.step = "quiz"


def on_submit_answer(q_index: int) -> None:
    selected = st.session_state.get(f"answer_{q_index}")
    questions = st.session_state.questions
    # 미선택, 이미 제출한 문항, 문항 생성 전(이전 화면의 버튼)이면 무시 (중복 클릭 방지)
    if selected is None or not questions or len(st.session_state.answers) != q_index or q_index >= len(questions):
        return
    question = questions[q_index]
    st.session_state.answers.append(AnswerRecord(question=question, selected_index=selected))


def on_next_question() -> None:
    # 현재 문항을 제출한 상태에서만 진행 (중복 클릭 시 문항 건너뜀 방지)
    if len(st.session_state.answers) != st.session_state.q_index + 1:
        return
    st.session_state.q_index += 1
    if st.session_state.q_index >= QUESTION_COUNT:
        st.session_state.completed_at = now_kst()
        st.session_state.step = "result"


def on_retry_same_team() -> None:
    _apply(QUIZ_STATE)
    st.session_state.error = None
    st.session_state.step = "scenario_select"


def on_clear_error() -> None:
    st.session_state.error = None


def on_build_pdf() -> None:
    team = current_team()
    data = ReportData(
        team=team,
        scenario=st.session_state.scenario,
        records=st.session_state.answers,
        recommendations=st.session_state.recommendations or [],
        completed_at=st.session_state.completed_at or now_kst(),
        participant_name=st.session_state.participant_name,
        participant_role=st.session_state.participant_role,
    )
    try:
        st.session_state.pdf_bytes = build_report(data)
        st.session_state.pdf_name = report_filename(team.name, data.completed_at)
        st.session_state.pdf_error = None
    except ReportError as e:
        st.session_state.pdf_error = str(e)


def on_clear_search() -> None:
    st.session_state.team_query = ""
    st.session_state.site_filter = "전체"


# ── 공통 ──────────────────────────────────────────────


def run_llm(spinner_text: str, fn, *args):
    """LLM 호출. 실패 시 오류 화면과 [다시 시도] 버튼을 보여주고 None을 반환한다 (자동 재호출 없음)."""
    if st.session_state.error is None:
        try:
            with st.spinner(spinner_text):
                return fn(*args)
        except llm.LLMError as e:
            st.session_state.error = str(e)
    ui.error_state("AI 응답을 받지 못했습니다", st.session_state.error)
    _, col, _ = st.columns([2, 1, 2])
    col.button("다시 시도", on_click=on_clear_error, key="retry_llm", type="primary", width="stretch")
    return None


def render_sidebar() -> None:
    settings = load_settings()
    with st.sidebar:
        st.markdown("### 진행 현황")
        st.markdown(f"**단계** · {STEPS[st.session_state.step]}")
        st.markdown(f"**팀** · {st.session_state.team_name or '-'}")
        if st.session_state.participant_name:
            st.markdown(f"**응시자** · {ui.md(st.session_state.participant_name)} ({st.session_state.participant_role})")
        scenario = st.session_state.scenario
        st.markdown(f"**시나리오** · {ui.md(scenario.title) if scenario else '-'}")
        if st.session_state.step == "quiz" and st.session_state.questions:
            st.markdown(f"**점수** · {quiz.score(st.session_state.answers)} / {len(st.session_state.answers)}")
        st.button("처음으로", on_click=reset, width="stretch", key="sidebar_home")
        st.divider()
        if settings.use_mock_llm:
            st.caption("🧪 Mock 모드 · 더미 데이터로 동작 중입니다.")
        else:
            st.caption(f"🤖 AI 모델 · {settings.openai_model}")
            if not settings.openai_api_key:
                st.warning("OPENAI_API_KEY가 설정되지 않았습니다.")


# ── 화면 ──────────────────────────────────────────────


def _team_grid(teams, n_cols: int) -> None:
    cols = st.columns(n_cols)
    for i, team in enumerate(teams):
        cols[i % n_cols].button(
            team.name,
            key=f"team_{team.name}",
            type="primary" if team.name == st.session_state.picked_team else "secondary",
            on_click=on_pick_team,
            args=(team.name,),
            width="stretch",
        )


def render_team_select() -> None:
    ui.section("훈련할 팀을 선택해 주십시오", "팀명 일부로 검색하거나 사업장으로 범위를 좁힐 수 있습니다.")
    col_query, col_site = st.columns([3, 2], vertical_alignment="bottom")
    query = col_query.text_input(
        "팀 검색", key="team_query", placeholder="🔍  팀명을 입력해 주십시오 (예: 정비, 품질, 도금)",
        label_visibility="collapsed",
    )
    site = col_site.radio("사업장", SITES, horizontal=True, key="site_filter", label_visibility="collapsed")
    teams = search_teams(query, site)

    if not teams:
        ui.empty_state("🔎", "검색 결과가 없습니다", f"'{query}'에 해당하는 팀이 없습니다. 검색어 또는 사업장을 확인해 주십시오.")
        _, col, _ = st.columns([2, 1, 2])
        col.button("검색 초기화", on_click=on_clear_search, width="stretch")
    else:
        by_site = {s: [t for t in teams if t.site == s] for s in SITES[1:]}
        shown = [s for s, ts in by_site.items() if ts]
        site_cols = st.columns(len(shown), gap="large")
        for col, s in zip(site_cols, shown):
            with col:
                ui.html(f'<div class="site-head">{s}<span>{len(by_site[s])}개 팀</span></div>')
                _team_grid(by_site[s], 2 if len(shown) > 1 else 3)

    st.write("")
    with st.container(border=True):
        picked = get_team(st.session_state.picked_team) if st.session_state.picked_team else None
        ui.picked_team(picked.label if picked else None)
        col_name, col_role, col_go = st.columns([2, 1, 1.3], vertical_alignment="bottom")
        col_name.text_input("응시자 이름 (선택)", key="participant_name_input", value=st.session_state.participant_name,
                            placeholder="보고서에 기재됩니다")
        col_role.selectbox("직책", ROLES, key="participant_role_input", index=ROLES.index(st.session_state.participant_role))
        col_go.button("시나리오 생성하기", type="primary", on_click=on_select_team, disabled=picked is None,
                      width="stretch")


def render_scenario_select() -> None:
    team = current_team()
    col_title, col_regen = st.columns([5, 1], vertical_alignment="bottom")
    with col_title:
        ui.section(f"{team.label} 모의훈련 시나리오", "심사를 진행할 시나리오를 하나 선택해 주십시오.")
    col_regen.button("🔄 다시 생성", on_click=on_regenerate_scenarios, width="stretch")

    cache = st.session_state.scenario_cache
    if team.name not in cache:
        scenarios = run_llm("심사위원이 시나리오를 준비하고 있습니다...", llm.generate_scenarios, team)
        if scenarios is None:
            return
        cache[team.name] = scenarios

    cols = st.columns(2, gap="medium")
    for i, sc in enumerate(cache[team.name]):
        with cols[i % 2].container(border=True):
            ui.scenario_card(sc)
            st.button("이 시나리오로 심사 시작", key=f"start_{sc.id}", on_click=on_start_quiz, args=(i,),
                      type="primary", width="stretch")


def render_quiz() -> None:
    team = current_team()
    scenario = st.session_state.scenario
    if st.session_state.questions is None:
        questions = run_llm("심사위원이 질의를 준비하고 있습니다...", llm.generate_quiz, team, scenario)
        if questions is None:
            return
        st.session_state.questions = questions

    q_index = st.session_state.q_index
    question = st.session_state.questions[q_index]
    answers = st.session_state.answers
    answered = len(answers) > q_index

    with st.expander("📋 시나리오 다시 보기", expanded=False):
        ui.scenario_summary(scenario)
    ui.score_bar(q_index, len(answers), quiz.score(answers))
    st.progress(len(answers) / QUESTION_COUNT)
    ui.question_card(q_index, question.area, question.question)

    if not answered:
        st.radio(
            "답을 선택해 주십시오",
            options=list(range(len(question.options))),
            format_func=lambda i: ui.md(ui.option_label(i, question.options[i])),
            index=None,
            key=f"answer_{q_index}",
            label_visibility="collapsed",
        )
        _, col = st.columns([3, 1])
        clicked = col.button("답변 제출", type="primary", on_click=on_submit_answer, args=(q_index,),
                             width="stretch")
        if clicked and st.session_state.get(f"answer_{q_index}") is None:
            st.warning("답을 선택한 뒤 제출해 주십시오.")
        return

    record = answers[q_index]
    ui.answered_options(question.options, record.selected_index, question.answer_index)
    ui.feedback(
        record.is_correct,
        ui.option_label(question.answer_index, question.options[question.answer_index]),
        question.explanation,
        question.iso_clause,
    )
    _, col = st.columns([3, 1])
    label = "결과 보기" if q_index + 1 >= QUESTION_COUNT else "다음 문항"
    col.button(label, type="primary", on_click=on_next_question, width="stretch")


def render_result() -> None:
    team = current_team()
    answers = st.session_state.answers
    points = quiz.score(answers)
    label, meaning = quiz.grade(points)

    ui.section(f"{team.label} 모의심사 결과", f"시나리오 · [{st.session_state.scenario.category}] {st.session_state.scenario.title}")
    col_score, col_grade, col_chart = st.columns([1, 1, 2.2], gap="medium")
    with col_score.container(border=True, key="metric_score"):
        st.metric("총점", f"{points} / {QUESTION_COUNT}")
        st.caption(f"정답률 {points / QUESTION_COUNT:.0%}")
    with col_grade.container(border=True, key="metric_grade"):
        st.metric("등급", label)
        st.caption(meaning)
    ui.metric_accent("metric_score", ui.NAVY)
    ui.metric_accent("metric_grade", ui.GRADE_COLORS.get(label, ui.NAVY))
    with col_chart.container(border=True):
        st.markdown("**영역별 정답률**")
        ui.area_bars(quiz.area_stats(answers))

    col_wrong, col_rec = st.columns(2, gap="large")
    with col_wrong:
        wrong = [(i, r) for i, r in enumerate(answers, 1) if not r.is_correct]
        st.markdown(f"#### 오답 노트 ({len(wrong)}문항)")
        if not wrong:
            ui.empty_state("🎉", "모든 문항을 맞혔습니다", "현재 수준을 유지할 수 있도록 정기 훈련을 이어가십시오.")
        for i, r in wrong:
            q = r.question
            with st.expander(ui.md(f"Q{i}. [{q.area}] {q.question}")):
                ui.answered_options(q.options, r.selected_index, q.answer_index)
                st.caption(ui.md(f"{q.explanation} (ISO 22301 {q.iso_clause})"))

    with col_rec:
        st.markdown("#### 개선 권고사항")
        if st.session_state.recommendations is None:
            try:
                with st.spinner("개선 권고사항을 작성하고 있습니다..."):
                    st.session_state.recommendations = llm.generate_recommendations(
                        team, st.session_state.scenario, answers)
            except llm.LLMError as e:
                st.session_state.recommendations = []
                st.warning(f"개선 권고사항을 생성하지 못했습니다. 보고서에는 권고사항 없이 기재됩니다. ({e})")
        ui.recommendations(st.session_state.recommendations)

    st.write("")
    with st.container(key="pdf_actions"):
        st.markdown("**📄 결과보고서**  \n훈련 결과를 갱신심사 증빙용 PDF로 저장합니다.")
        col_build, col_dl = st.columns(2)
        col_build.button("결과보고서 PDF 생성", type="primary", on_click=on_build_pdf, width="stretch")
        if st.session_state.pdf_bytes:
            col_dl.download_button(
                "⬇️ PDF 다운로드", data=st.session_state.pdf_bytes, file_name=st.session_state.pdf_name,
                mime="application/pdf", type="primary", width="stretch",
            )
        if st.session_state.pdf_error:
            st.error(st.session_state.pdf_error)

    st.write("")
    col_home, col_retry, _ = st.columns([1, 1.4, 2])
    col_home.button("처음으로", key="restart", on_click=reset, width="stretch")
    col_retry.button("같은 팀 다른 시나리오로 재도전", on_click=on_retry_same_team, width="stretch")


def main() -> None:
    st.set_page_config(page_title="BCMS 모의심사", page_icon="🛡️", layout="wide")
    init_state()
    ui.inject_css()
    ui.header()
    ui.stepper(st.session_state.step)
    render_sidebar()
    {
        "team_select": render_team_select,
        "scenario_select": render_scenario_select,
        "quiz": render_quiz,
        "result": render_result,
    }[st.session_state.step]()


main()
