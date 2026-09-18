from copaw.agents.knowledge_prefetch import (
    _fallback_plan,
    _metadata_result,
    _sanitize_rewrites,
    build_tool_policy,
)


def test_fallback_routes_selected_kb_metadata_without_retrieval():
    plan = _fallback_plan('当前选了哪些知识库？', has_selected_kbs=True)

    assert plan['intent'] == 'knowledge_meta'
    assert plan['source_scope'] == 'open'


def test_fallback_only_uses_memory_for_explicit_memory_intent():
    memory = _fallback_plan('我们之前决定了什么？', has_selected_kbs=True)
    kb = _fallback_plan('教师账号如何导入？', has_selected_kbs=True)

    assert memory['intent'] == 'memory_query'
    assert kb['intent'] == 'knowledge_qa'


def test_metadata_result_contains_authorized_selected_kbs():
    result = _metadata_result(
        {'intent': 'knowledge_meta', 'source_scope': 'open'},
        ['kb-1'],
        ['教师手册'],
    )

    assert result['evidence_status'] == 'metadata_answer'
    assert result['selected_knowledge_bases'] == [
        {'kb_id': 'kb-1', 'kb_name': '教师手册'},
    ]


def test_tool_policy_isolates_memory_and_knowledge_sources():
    memory = build_tool_policy(
        intent='memory_query',
        source_scope='open',
        evidence_status='no_evidence',
        enable_agent=True,
        enable_skills=True,
    )
    evidence = build_tool_policy(
        intent='knowledge_qa',
        source_scope='open',
        evidence_status='strong_evidence',
        enable_agent=True,
        enable_skills=True,
    )
    failed = build_tool_policy(
        intent='knowledge_qa',
        source_scope='open',
        evidence_status='retrieval_error',
        enable_agent=True,
        enable_skills=False,
    )

    assert memory['allow_memory_search'] is True
    assert memory['allow_general_tools'] is False
    assert not any(evidence.values())
    assert failed['allow_general_tools'] is True
    assert failed['allow_kb_fallback'] is True
    assert failed['kb_fallback_max_calls'] == 1


def test_selected_only_no_evidence_does_not_open_alternative_sources():
    policy = build_tool_policy(
        intent='knowledge_qa',
        source_scope='selected_kb_only',
        evidence_status='no_evidence',
        enable_agent=True,
        enable_skills=True,
    )

    assert policy['allow_general_tools'] is False
    assert policy['allow_skills'] is False
    assert policy['allow_memory_search'] is False


def test_rewrite_guard_preserves_models_dates_and_negation():
    standalone, expanded = _sanitize_rewrites(
        'X-100 在 2026 年不得启用哪些功能？',
        '设备有哪些限制？',
        ['设备限制', 'X-200 在 2026 年的限制'],
    )

    assert standalone == 'X-100 在 2026 年不得启用哪些功能？'
    assert expanded == ['设备限制 X-100 2026 不得']
