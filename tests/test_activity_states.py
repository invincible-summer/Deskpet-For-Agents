import json
import unittest

from agents.codex import CodexFile
from agents.claude import ClaudeFile
from agents.models import AgentKind, Mode, Status, parse_mode, Snapshot
from agents.summarize import shorten, fmt_command
from pet.labels import activity_text


def feed(state, payload, timestamp=100):
    state.feed(json.dumps(dict(type='response_item', timestamp=timestamp, payload=payload)))


def test_codex_new_tool_after_completed_turn_and_long_silence():
    state = CodexFile('unused')
    state.feed(json.dumps(dict(type='event_msg', timestamp=90, payload={'type': 'task_complete'})))
    feed(state, dict(type='function_call', name='exec_command', arguments='{"cmd":"pytest -q"}'))
    obs = state.observation(1000, {})
    assert obs.status == Status.WORKING
    assert 'pytest -q' in obs.summary


def test_codex_question_and_matching_reply():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='functions.request_user_input', call_id='q', arguments=json.dumps({'questions':[{'question':'选哪个？', 'options':[{'label':'A'}, {'label':'B'}]}]})))
    assert state.observation(1000, {}).status == Status.INPUT
    assert '等待选择回复' in state.observation(1000, {}).summary
    feed(state, dict(type='function_call_output', call_id='other', output='ok'))
    assert state.observation(1000, {}).status == Status.INPUT
    feed(state, dict(type='function_call_output', call_id='q', output='A'))
    assert state.observation(1000, {}).status == Status.WORKING


def test_goal_and_structured_collaboration_mode():
    assert parse_mode({'mode':'plan'})[0] == Mode.PLAN
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='create_goal', arguments='{"objective":"Build"}'))
    assert state.observation(1000, {}).mode == Mode.GOAL
    feed(state, dict(type='function_call', name='update_goal', arguments='{"status":"complete"}'))
    assert state.observation(1000, {}).mode != Mode.GOAL


def test_claude_string_user_and_question():
    state = ClaudeFile('unused')
    state.feed(json.dumps({'type':'user', 'timestamp':100, 'message':{'content':'Fix tests'}}))
    assert state.observation(1000, {}).status == Status.WORKING
    state.feed(json.dumps({'type':'assistant', 'timestamp':101, 'message':{'content':[{'type':'tool_use', 'name':'AskUserQuestion', 'id':'q', 'input':{'questions':[{'question':'Which?', 'options':[{'label':'A'}]}]}}]}}))
    assert state.observation(1000, {}).status == Status.INPUT
    assert 'Which?' in state.observation(1000, {}).summary


def test_bounded_display_preserves_command_syntax():
    for limit in (1, 2, 40, 120):
        assert len(shorten('x' * 200, limit)) <= limit
        assert len(fmt_command('x' * 200, limit)) <= limit
    snap = Snapshot(key="test", kind=AgentKind.CODEX, source="windows", pid=1, status=Status.WORKING, summary='rg foo_bar /tmp/my_path')
    assert 'foo_bar /tmp/my_path' in activity_text(snap)
    assert 'foo_bar' not in activity_text(snap, False)


def test_codex_error_does_not_resurrect_and_new_input_clears_error():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='exec_command', arguments='{}'))
    feed(state, dict(type='error', message='failed'), 101)
    assert state.observation(102, {}).status == Status.ERROR
    assert state.observation(200, {}).status == Status.IDLE
    feed(state, dict(type='message', role='user', content=[]), 103)
    assert state.observation(104, {}).status == Status.WORKING
    assert 'exec_command' not in state.observation(104, {}).summary


def test_codex_async_question_survives_ack_until_user_reply():
    state = CodexFile('unused')
    feed(state, dict(type='function_call', name='functions.request_user_input_async',
                    call_id='q', arguments=json.dumps({'questions': [{'title': 'Which?', 'options': ['A', 'B']}]})))
    feed(state, dict(type='function_call_output', call_id='q', output='queued'), 101)
    assert state.observation(1000, {}).status == Status.INPUT
    feed(state, dict(type='message', role='user', content=[{'text':'A'}]), 102)
    assert state.observation(103, {}).status == Status.WORKING


def test_claude_question_only_matching_result_or_completion_clears():
    state = ClaudeFile('unused')
    def event(t, **kwargs):
        state.feed(json.dumps(dict(type=t, timestamp=100, **kwargs)))
    def ask():
        event('assistant', message={'content': [{'type':'tool_use', 'name':'AskUserQuestion', 'id':'q', 'input':{'questions':[{'question':'Which?'}]}}]})
    ask()
    event('user', message={'content':[{'type':'tool_result', 'tool_use_id':'other'}]})
    assert state.observation(101, {}).status == Status.INPUT
    event('user', message={'content':[{'type':'tool_result', 'tool_use_id':'q'}]})
    assert state.observation(101, {}).status == Status.WORKING
    ask()
    event('result', result='finished')
    assert state.observation(101, {}).status == Status.DONE


def test_goal_is_visible_in_both_display_modes_and_bounded():
    snap = Snapshot(key='test', kind=AgentKind.CODEX, source='windows', pid=1,
                    status=Status.WORKING, mode=Mode.GOAL,
                    summary='rg foo_bar /tmp/my_path ' + 'x' * 200)
    for detail in (False, True):
        assert 'Goal' in activity_text(snap, detail)
        for limit in (1, 2, 20, 80, 120):
            assert len(activity_text(snap, detail, limit)) <= limit


def test_codex_wrapper_displays_literal_command_without_executing_code():
    state = CodexFile('unused')
    feed(state, dict(type='custom_tool_call', name='functions.exec',
                    input='text(await tools.exec_command({cmd: "rg foo_bar /tmp/my_path", max_output_tokens: 1000}));'))
    summary = state.observation(1000, {}).summary
    assert 'rg foo_bar /tmp/my_path' in summary
    assert 'max_output_tokens' not in summary


def load_tests(loader, tests, pattern):
    # This repository runs unittest discovery, including these regression functions.
    return unittest.TestSuite(unittest.FunctionTestCase(value)
                              for name, value in sorted(globals().items())
                              if name.startswith('test_') and callable(value))
